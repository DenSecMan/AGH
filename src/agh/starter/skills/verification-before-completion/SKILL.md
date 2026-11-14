---
name: verification-before-completion
description: "Check evidence before claiming a task is done: the original requirement, test results, and the final diff. Use before any report of success, fixed, passing or complete."
---

# Verification before completion

A claim of success needs evidence you saw in this session. Run these checks right before you report.

## 1. The requirement

1. Reread the user's original request, not your summary of it.
2. For each thing it asked for, name the evidence that it is done: a test, a command's output, or a file you reread.
   Anything without evidence is not done; say so.

## 2. The tests

1. Run the relevant test command now, after your last edit. A run from before an edit does not count.
2. Record the exact command, the exit code and the pass/fail counts.
3. Failures: say whether they existed before your change (from your orientation baseline) or are new. New failures
   mean the task is not done.
4. Also run the repository's lint and typecheck commands if it has them. Fix errors and warnings your change
   introduced; leave pre-existing ones alone and mention them.
5. No tests apply? Run the code another way (the CLI, a short script, the server with process) and show the result.

## 3. The diff

1. Run git diff (and git status for new files). Read every hunk.
2. Remove anything unrelated to the request: debug prints, stray formatting, commented-out code, temporary files.
3. Check that you did not touch pre-existing user changes, generated files or secrets.

## 4. Review

For a non-trivial change to code, delegate to `reviewer` with the requirement, the list of changed files and the diff.
Fix confirmed defects, rerun the tests, and mention suspicions you chose not to act on.

Skip the reviewer for documentation-only edits, generated-output refreshes and other small mechanical changes; checks
1-3 are enough there. Review once. After fixing a confirmed finding, verify the fix yourself by rereading the changed
lines; do not delegate a second review unless the fix was itself a non-trivial code change.

## 5. Report

Use words that match the evidence. "Tests pass" only if you saw them pass after the last edit. "Should work" is not a
result: either verify it or list it as unverified.
