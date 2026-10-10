TASK: implement pending roadmap steps. A STEP is a whole section/phase of ROADMAP.md — not one checklist line. One run works a step to completion, and keeps going into the next step while budget remains.
1. `git fetch origin && git checkout -B {branch_prefix}-<short-slug> origin/{default_branch}` (slug from the step name; always use this exact `{branch_prefix}-` prefix so branch names never collide with earlier runs).
2. Read ROADMAP.md. Pick the FIRST pending STEP: the earliest step / phase / section that still has any unchecked `- [ ]` item or unmarked deliverable. Skip steps that need human decisions, credentials, paid services or hardware; take the next one.
3. If there is no pending step, do nothing and finish with marker NO_PENDING_STEPS.
4. Implement that ENTIRE step in this run: every deliverable and EVERY unchecked acceptance criterion in its section, with tests where the project has a test setup. Run the project's tests/typecheck/lint. Mark the step done in ROADMAP.md following its existing convention — all of its items, not one line.
   - Ship whole steps. A PR that ticks a single criterion of a multi-criterion step, or whose title says "(partial)", is the exact failure this rule forbids.
   - Do not stop after a small change because the change is done: if the step has more criteria, keep working. Commits and pushes are how you checkpoint, not how you finish.
   - If the step cannot be finished in the time budget, push your commits, do NOT open a PR, and finish with marker INCOMPLETE so the next run resumes this same session and worktree.
5. Push and open ONE PR for the step: `gh pr create -R {slug} --base {default_branch} --title "<step>" --body "<what/why/how tested>"`. If the step lists issues it closes, the body has a `Closes #n` line for EACH issue the step fully resolves, so the squash-merge closes them. For an issue the step resolves only partly, comment on it (`gh issue comment`) naming which items this PR resolved and which remain.
6. If budget clearly remains (roughly more than 5 minutes), go back to step 2 and take the NEXT pending step in the same run: a fresh branch and its own PR per step. Base a step's branch on {default_branch}, unless it depends on a step you just implemented in this same run — then base it on that step's branch and open the PR against that branch too. Repeat until the budget is nearly spent or no pending step remains.

Status markers (the LAST line of your final answer):
- PR_OPENED — the step is complete and its PR is open (or, at the end of the run, all work done so far is committed and pushed)
- NO_PENDING_STEPS — no pending step left
- INCOMPLETE — the step is not finished and needs another run to resume it
- BLOCKED — a real blocker (human decision, credentials, external access)
Markers: PR_OPENED | NO_PENDING_STEPS | INCOMPLETE | BLOCKED
