TASK: review and fix open PR #{pr} ({branch}) of {slug}; iteration {iteration} of {max_iter}.
This is a FRESH, independent review. Ignore anything you may infer about earlier review rounds: do not read earlier PR comments, review notes, logs/ directories or commit messages to decide what is "already reviewed". Judge only the code as it is now.

SEVERITY
- Medium+ (blocks merge): data loss, data corruption, security weakness, crash, or wrong behavior in a NORMAL use case; broken contracts; missing tests for risky logic.
- Iteration 3 and later: Medium+ is ONLY data loss, data corruption, security weakness, crash, or wrong behavior in a normal use case. Edge cases that need hand-edited files, hostile/concurrent actors, unusual mounts or misconfiguration are Low.
- Everything else is Low (style, hardening, rare edge cases, test hygiene, nitpicks).
- SCOPE (always Medium+, at every iteration): if this PR implements a ROADMAP.md step (not a roadmap-authoring, docs or follow-up PR) and that step's section still has unchecked `- [ ]` items or unmet acceptance criteria after this PR, or the title says "partial", the step is incomplete. Fix it by implementing the remaining items of that step on this branch (skip only items that need a human, hardware or credentials; list those in the PR body), tick them in ROADMAP.md, and drop "(partial)" from the title (`gh pr edit {pr} -R {slug} --title ...`). A PR that ships a fraction of a step is never merged.

STEPS
1. `git fetch origin && git checkout -B {branch} origin/{branch}`.
2. Review the full PR diff against {default_branch} using the code-review skill at high effort (Skill tool: code-review, args "high"); also read the diff yourself. Write down ALL findings BEFORE changing any code, and classify each as Medium+ or Low using the SEVERITY rules. "Unsure" is not allowed: decide.
3. Branch on what you found:
   a) At least one real Medium+ finding: FIX ALL real findings, Medium+ AND Low (including small issues and nitpicks). Fixing the small ones reduces future Medium+ bugs. Skip a finding only if it truly cannot be fixed in this PR (needs a human decision, external access or a large redesign): then open a GitHub issue for it (`gh issue create -R {slug} --title ... --body ...`, mention PR #{pr}), and if it was Medium+ finish with BLOCKED. Run tests/typecheck, commit, push to {branch}. Finish with FIXED (never merge in the same run as fixes).
   b) Zero Medium+ findings, only Low ones: do NOT fix them. Open ONE follow-up issue listing all Low findings (`gh label create autopilot-followup -R {slug} --color ededed --description "Low-severity review leftovers" 2>/dev/null; gh issue create -R {slug} --title "Follow-ups from PR #{pr} review" --label autopilot-followup --body ...`). In the body, start with an `Area:` line naming the subsystem/code area the findings touch, then a checklist with one `- [ ]` item per finding naming the file and function. Roadmap mode later bundles these issues by area into multi-issue sweep steps. Then go to step 4.
   c) Zero findings: go to step 4.
4. Merge gate. Check CI for the LATEST commit only: `sha=$(git rev-parse HEAD)`, confirm `gh pr view {pr} -R {slug} --json headRefOid --jq .headRefOid` equals it. Poll `gh run list -R {slug} --commit $sha --json status,conclusion,name` (and `gh pr checks {pr} -R {slug}`) every 30s, up to 10 minutes, until every run for that SHA has completed. Results from older commits do not count. If the repo has no workflows, local tests passing is enough. If runs are still pending after 10 minutes or any failed, do not merge: finish with FIXED (pending/failed CI) so the next run retries.
5. If CI is green: `gh pr merge {pr} -R {slug} --squash --delete-branch`, then marker MERGED.

Your final answer must include the classification table (finding | Medium+ or Low | action taken: fixed / follow-up issue / blocked) before the marker.
Markers: MERGED | FIXED | BLOCKED
