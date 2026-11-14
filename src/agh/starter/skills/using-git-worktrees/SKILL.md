---
name: using-git-worktrees
description: "Set up or use an isolated Git worktree for code changes that need branch or workspace isolation. Use when the user requests a worktree, has a stated worktree preference, or parallel work needs isolation."
---

# Using Git worktrees

Adapted from [obra/superpowers using-git-worktrees](https://github.com/obra/superpowers/tree/main/skills/using-git-worktrees) (MIT), for AGH's workspace and tool permissions.

1. **Inspect the current workspace.** Check the repository root, current branch, working tree status, and whether this is already a linked worktree or submodule. Respect repository instructions and any stated worktree location.
2. **Choose isolation deliberately.** Use an existing worktree when available. Create one when the user requests it, has set that preference, or the task needs parallel branch isolation. Otherwise continue in the current checkout.
3. **Check the destination.** Do not overwrite an existing path or move the user's uncommitted work. If the worktree will be inside the repository, use a directory already ignored by Git. Do not edit `.gitignore` just to enable a worktree. If the sandbox cannot reach the chosen location, use the current checkout only when it is safe; otherwise explain the limit.
4. **Use allowed tools.** Create a worktree with `git worktree add` through the available shell only when permitted. AGH's Git tool is read-only; shell or sandbox approval rules still apply. Do not create, switch, or delete branches through an alternate route to bypass them.
5. **Orient the worktree.** Follow its `AGENTS.md`, README, and project setup instructions. Run the relevant baseline checks when the task and project instructions call for them; do not guess install commands or install dependencies by convention alone.
6. **Report the location.** Give the worktree path, branch, and any baseline result. If setup is blocked, say what blocked it and agree on a safe next step.

