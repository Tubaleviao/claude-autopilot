# config.json reference

Copy `config.example.json` to `config.json` and edit. `u5` = % of the current 5h window used, `u7` = % of the weekly limit used. Keys marked ✱ can be tuned by the weekly self-review, within the bounds shown.

## Projects
| Key | Default | Meaning |
|---|---|---|
| `projects` | – | Projects to work on, highest priority first. Each name is the folder name under `projects_dir`. |
| `projects_dir` | `~/projects` | Folder holding the project clones. |
| `project_opts` | `{}` | Per-project options, keyed by project name (see below). |
| `pr_scope` | `autopilot` | `autopilot`: only review your PRs on `autopilot/*` branches. Any other value: all your open PRs. |
| `streak_limit` | 5 | After this many PRs in a row in one project, prefer other projects for new work. |
| `starved_roadmap_days` | 3 | A project with no pending steps and no roadmap run for this long gets roadmap authoring first. |
| `exhausted_hours` ✱ | 24 (6–72) | A project with nothing to do is skipped for this long, or until its roadmap changes. |

### `project_opts`
| Key | Default | Meaning |
|---|---|---|
| `roadmap` | `ROADMAP.md` | Roadmap path in the repo. |
| `push_remote` | – | Work from a fork: push to this remote and open PRs against `origin`. |
| `merge` | `true` | `false`: never merge; a clean PR is left `READY` for you. |
| `author_roadmap` | `true` | `false`: never write new roadmap steps. |
| `depends_on` | – | Another configured project whose work this one needs; autopilot files issues there instead of blocking. |
| `context` | – | Text about that dependency, added to the prompts. |

## Pacing
| Key | Default | Meaning |
|---|---|---|
| `tail_minutes` ✱ | 60 (30–150) | Minimum time before a window ends when autopilot may start. Grows when weekly usage falls behind. |
| `ramp_gain` ✱ | 1.3 (1–2) | How fast the tail grows when behind. Higher = starts earlier. |
| `open_window_fill` ✱ | 0.4 (0.3–0.9) | Open a new window only when this share of every remaining window is needed to reach 100% weekly. |
| `pace_margin` ✱ | 4 (2–8) | Weekly cap: `u7` never goes above linear pace + this many points. |
| `u5_pace_margin` ✱ | 15 (5–30) | Inside a window `u5` stays ≤ elapsed share × 100 + this. |
| `min_window_headroom_pct` ✱ | 3 (1–10) | Don't start when less than this % of the window is left. |
| `max_u5` / `max_u7` | 97 / 98 | Hard stops for the window and the week, also in burn mode. |
| `default_k` ✱ | 0.12 (0.05–0.3) | Weekly % per 1% of a window, used until enough data in `usage-log.jsonl` to learn it. |
| `min_k_samples` | 5 | Not used. |
| `burn` | `false` | Spend as fast as possible, ignoring pacing (same as `--burn`). |

## Runs
| Key | Default | Meaning |
|---|---|---|
| `max_run_minutes` ✱ | 45 (20–60) | Time limit of one run. |
| `min_run_minutes` ✱ | 15 (10–25) | Don't start a run shorter than this. |
| `min_step_minutes` | 30 | Don't start a new implement/roadmap step with less time left (reviews and resumes still run). |
| `max_runs_per_tick` ✱ | 15 (3–30) | Back-to-back runs per cron tick. |
| `max_review_iterations` ✱ | 8 (4–12) | Review rounds on one PR before it is labeled `autopilot-stuck`. |
| `max_resumes` ✱ | 3 (1–5) | Times an interrupted run is resumed before giving up. |
| `resume_max_hours` | 48 | Drop a pending resume older than this. |

## Models
| Key | Default | Meaning |
|---|---|---|
| `model` | `sonnet` | Model for runs. |
| `model_by_mode` | roadmap, selfreview → `opus` | Model override per mode (`implement`, `review`, `roadmap`, `selfreview`). |
| `helper_model` | `haiku` | Model for small helpers (CI log summary, digest, precheck). |
| `precheck` | `true` | Ask the helper whether a roadmap step is pending before starting an implement run. |

## CI
| Key | Default | Meaning |
|---|---|---|
| `ci_timeout_minutes` | 30 | CI still pending after this → back to review. |
| `ci_settle_minutes` | 5 | With no checks reported after this, treat the PR as green. |
| `main_red_fix_hours` | 2 | Default branch red this long → next implement run fixes it first. |

## Notifications
| Key | Default | Meaning |
|---|---|---|
| `notify_telegram` | `true` | Send alerts via the Claude Code Telegram channel (`~/.claude/channels/telegram/`). Set `false` if you don't have one. |
| `digest_minutes` | 60 | Batch routine events into one message this often (0 = send each at once). |
| `digest_summary_min_events` | 6 | Digests with this many events are condensed by the helper model. |

## Self-review and cleanup
| Key | Default | Meaning |
|---|---|---|
| `selfreview_days` | 7 | Days between self-reviews. |
| `selfreview_min_runs` | 3 | Runs needed before a self-review happens. |
| `log_keep_days` | 14 | Delete run logs older than this. |
| `data_keep_days` | 90 | Delete `*.jsonl` rows and backups older than this. |
