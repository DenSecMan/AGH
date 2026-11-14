---
name: code-review
description: "Review a code change for correctness, regressions, security and maintainability, and report concrete findings. Use when asked to review a diff, branch or set of changed files, or before finishing a non-trivial change."
---

# Code review

Review the change, not the whole codebase. Read-only: report findings, do not fix them.

## 1. Scope

1. Know the requirement. If none was given, infer it from the change and say so.
2. Get the diff: git diff (unstaged), git diff --staged, or git diff BASE..HEAD for a branch.
3. For each changed function, read the whole function and its callers (search_files for the name).

## 2. Check, in this order

1. Correctness: does it do what the requirement asks? Off-by-one, wrong conditions, missing None/empty/error
   cases, wrong types, async misuse, resource leaks.
2. Regressions: callers that relied on the old behaviour, changed signatures or defaults, removed handling, tests
   changed to fit the code rather than the requirement.
3. Security: untrusted input reaching shell, SQL, paths or eval; secrets in code or logs; weakened permission checks.
4. Tests: is the new behaviour tested? Would the test fail without the change?
5. Maintainability: duplication of existing helpers, names that mislead, conventions the repository documents.
   Only report these when they matter; style nits go last or not at all.

## 3. Report

Most severe first. For each finding: file and line, one-sentence defect, and a concrete failure scenario (inputs or
state, then the wrong result). Mark each as confirmed (you traced it) or suspected. End with a verdict: meets the
requirement, meets it with issues, or does not. Report nothing you cannot point to in the code.
