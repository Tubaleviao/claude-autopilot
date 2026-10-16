Stdin holds a project's roadmap file. Decide whether an unattended coding bot has a roadmap step it can implement now.
- A STEP is a whole section / phase of the roadmap. It is PENDING if it has any unchecked `- [ ]` item, or a deliverable not marked done (done markers vary: `- [x]`, ✅, "Status: implemented/done/concluído", strikethrough, a "Done" section).
- Skip steps that need a human decision, credentials, paid services, hardware or manual acceptance; also skip steps explicitly marked deferred/later/blocked.
- If in doubt about whether a step is done or doable, count it as pending.
Answer with a one-line reason, then exactly one final line:
PENDING: <title of the first doable pending step>
or
NONE
