---
name: finishing-development-branch
description: "Review completed code work and report its branch state and verification. Use when implementation is ready to hand back, integrate, or clean up."
---

# Finishing a development branch

Adapted from [obra/superpowers finishing-a-development-branch](https://github.com/obra/superpowers/tree/main/skills/finishing-a-development-branch) (MIT), for AGH's permission model.

1. **Check the result.** Review the final diff and working tree. Confirm which project checks ran on this version; report checks not run without claiming they passed.
2. **Identify ownership.** Check the current branch and worktree. Determine the base branch from instructions, history, or the user; do not assume `main` or `master`.
3. **Hand back by default.** If the user asked for implementation only, leave the branch and worktree in place. Summarize the changes, verification, and current branch state.
4. **Follow integration instructions.** Merge, push, create a pull request, commit, or delete a branch only when the user has explicitly requested or already authorized that action. Use available tools and their approval rules; do not imply AGH's read-only Git tool can perform writes.
5. **Clean up cautiously.** Remove only a worktree created for this task when the user has authorized cleanup and its status is clean. Never force removal or discard uncommitted or untracked files. Leave host-managed workspaces alone.

