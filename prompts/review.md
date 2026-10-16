TASK: review and fix open PR #{pr} ({branch}) of {slug}; iteration {iteration} of {max_iter}.
This is a FRESH, independent review. Ignore anything you may infer about earlier review rounds: do not read earlier PR comments, review notes, logs/ directories or commit messages to decide what is "already reviewed". Judge only the code as it is now.

SEVERITY
- Medium+ (blocks merge): data loss, data corruption, security weakness, crash, or wrong behavior in a NORMAL use case; broken contracts; missing tests for risky logic.
- Iteration 3 and later: Medium+ is ONLY data loss, data corruption, security weakness, crash, or wrong behavior in a normal use case. Edge cases that need hand-edited files, hostile/concurrent actors, unusual mounts or misconfiguration are Low.
- Trust boundary (always Medium+, at every iteration): a server/host that enforces a rule only on the client, trusts client-reported state it could check itself, or accepts unvalidated network input is a security weakness, even when exploiting it needs a modified client.
- Everything else is Low (style, hardening, rare edge cases, test hygiene, nitpicks).
- SCOPE (always Medium+, at every iteration): if this PR implements a {roadmap} step (not a roadmap-authoring, docs or follow-up PR) and that step's section still has unchecked `- [ ]` items or unmet acceptance criteria after this PR, or the title says "partial", the step is incomplete. Fix it by implementing the remaining items of that step on this branch (skip only items that need a human, hardware or credentials; list those in the PR body), tick them in {roadmap}, and drop "(partial)" from the title (`gh pr edit {pr} -R {slug} --title ...`). A PR that ships a fraction of a step is never merged.

EXISTING ISSUES FOR THIS PR
{existing_issues}

CI STATUS (from the driver)
{ci_context}
A CI failure or a failed merge reported here is a Medium+ finding: fix it under 3a (or, if it cannot be fixed in this repo, follow the BLOCKED / WAITING rules).

STEPS
1. `git fetch origin && git checkout -B {branch} origin/{branch}`.
2. Review the full PR diff against {default_branch} using the code-review skill at high effort (Skill tool: code-review, args "high"); also read the diff yourself. Write down ALL findings BEFORE changing any code, and classify each as Medium+ or Low using the SEVERITY rules. "Unsure" is not allowed: decide.
3. Branch on what you found:
   a) At least one real Medium+ finding: at iterations 1-2 FIX ALL real findings, Medium+ AND Low (including small issues and nitpicks); from iteration 3 on fix ONLY Medium+ findings and put the Lows into the follow-up issue described in b). Fixing the small ones early reduces future Medium+ bugs, but late rounds of Low fixes only cause churn. Skip a finding only if it truly cannot be fixed in this PR (needs a human decision, external access or a large redesign): then reuse a matching issue from EXISTING ISSUES or open a GitHub issue for it (`gh issue create -R {slug} --title ... --body ...`, mention PR #{pr}), and if it was Medium+ finish with BLOCKED. Run tests/typecheck, commit, push to {branch}. Finish with FIXED.
   b) Zero Medium+ findings, only Low ones: do NOT fix them. If EXISTING ISSUES names a follow-up issue, update that one; otherwise open ONE follow-up issue listing all Low findings (`gh label create autopilot-followup -R {slug} --color ededed --description "Low-severity review leftovers" 2>/dev/null; gh issue create -R {slug} --title "Follow-ups from PR #{pr} review" --label autopilot-followup --body ...`). In the body, start with an `Area:` line naming the subsystem/code area the findings touch, then a checklist with one `- [ ]` item per finding naming the file and function. Roadmap mode later bundles these issues by area into multi-issue sweep steps. Then go to step 4.
   c) Zero findings: go to step 4.
4. Run the project's tests/typecheck locally on this branch. A failure is a Medium+ finding: handle it under 3a. If they pass, push nothing and finish with CLEAN.
Do NOT wait for CI and do NOT merge (no `gh pr merge`, no polling `gh run list`/`gh pr checks`). After CLEAN the driver waits for CI on the head commit and merges it, or sends the PR back for another review with the CI failure under CI STATUS.

Your final answer must include the classification table (finding | Medium+ or Low | action taken: fixed / follow-up issue / blocked) before the marker.
Markers: CLEAN | FIXED | WAITING | BLOCKED (WAITING only when PROJECT OVERRIDES allow it)
